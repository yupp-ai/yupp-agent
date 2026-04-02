import SwiftUI

struct SessionListView: View {
    @EnvironmentObject var appState: AppState
    @EnvironmentObject var viewModel: SessionsViewModel

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                // Filter chips
                // Status filter
                HStack(spacing: 4) {
                    FilterChip(label: "All", isActive: !viewModel.showOnlyActive && viewModel.filterTrigger == nil) {
                        viewModel.showOnlyActive = false
                        viewModel.filterTrigger = nil
                    }
                    FilterChip(label: "Active", isActive: viewModel.showOnlyActive) {
                        viewModel.showOnlyActive = true
                        viewModel.filterTrigger = nil
                    }
                    Divider().frame(height: 14)
                    ForEach(TriggerType.allCases, id: \.self) { trigger in
                        FilterChip(label: trigger.label, isActive: viewModel.filterTrigger == trigger) {
                            viewModel.showOnlyActive = false
                            viewModel.filterTrigger = viewModel.filterTrigger == trigger ? nil : trigger
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)

                if viewModel.isLoading && viewModel.sessions.isEmpty {
                    HStack {
                        Spacer()
                        ProgressView()
                            .scaleEffect(0.7)
                        Text("Loading sessions...")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                    }
                    .padding(.vertical, 20)
                }

                ForEach(viewModel.groupedSessions, id: \.0) { group, sessions in
                    Section {
                        ForEach(sessions) { session in
                            SessionRowView(session: session, isSelected: session.sessionId == appState.selectedSessionId)
                                .onTapGesture {
                                    appState.selectSession(session.sessionId)
                                }
                                .contextMenu {
                                    Button("Copy Session ID") {
                                        NSPasteboard.general.clearContents()
                                        NSPasteboard.general.setString(session.sessionId, forType: .string)
                                    }
                                }
                        }
                    } header: {
                        Text(group)
                            .font(.caption)
                            .fontWeight(.semibold)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 16)
                            .padding(.top, 12)
                            .padding(.bottom, 4)
                    }
                }
            }
        }
        .task {
            await viewModel.loadIfNeeded()
        }
        .onChange(of: appState.authService.isAuthenticated) { _, isAuth in
            // Reload sessions when user signs in
            if isAuth {
                Task { await viewModel.refresh() }
            }
        }
        .onChange(of: appState.authService.userId) { _, uid in
            // Reload when userId resolves (happens async after sign-in)
            if !uid.isEmpty {
                Task { await viewModel.refresh() }
            }
        }
        .onChange(of: appState.selectedSessionId) { _, _ in
            if appState.currentView != .chat {
                appState.currentView = .chat
            }
        }
    }
}

struct FilterChip: View {
    let label: String
    let isActive: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(label)
                .font(.caption)
                .fontWeight(isActive ? .semibold : .regular)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(isActive ? Color.accentColor.opacity(0.15) : Color.clear)
                .foregroundStyle(isActive ? Color.accentColor : .secondary)
                .cornerRadius(6)
        }
        .buttonStyle(.plain)
    }
}
