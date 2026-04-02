import SwiftUI

struct SidebarView: View {
    @EnvironmentObject var appState: AppState

    /// Most common agent from the last 10 sessions.
    private var defaultAgent: String? {
        let recent = appState.sessionsViewModel.sessions.prefix(10)
        var counts: [String: Int] = [:]
        for session in recent {
            let name = session.agentName ?? session.agentId ?? ""
            if !name.isEmpty { counts[name, default: 0] += 1 }
        }
        return counts.max(by: { $0.value < $1.value })?.key
    }

    var body: some View {
        VStack(spacing: 0) {
            // App title
            HStack {
                Text("AHS")
                    .font(.title2)
                    .fontWeight(.bold)
                Spacer()
            }
            .padding(.horizontal, 16)
            .padding(.top, 10)
            .padding(.bottom, 4)

            // Search button
            Button {
                appState.showSearchOverlay = true
            } label: {
                HStack {
                    Image(systemName: "magnifyingglass")
                        .foregroundStyle(.secondary)
                    Text("Search")
                        .foregroundStyle(.secondary)
                    Spacer()
                    Text("Cmd+K")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(.quaternary)
                        .cornerRadius(4)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(.bar)
                .cornerRadius(8)
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 12)
            .padding(.top, 12)

            // New Session
            HStack(spacing: 6) {
                // "New Session" button — creates with default agent or opens picker
                Button {
                    if let agent = defaultAgent {
                        Task { await appState.createNewSession(agentId: agent) }
                    } else {
                        appState.showNewSessionPicker = true
                    }
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "plus.circle.fill")
                        Text("New Session")
                    }
                    .font(.subheadline.weight(.medium))
                }
                .buttonStyle(.plain)

                Spacer()

                // Agent bubble — click opens picker
                Button {
                    appState.showNewSessionPicker = true
                } label: {
                    Text(defaultAgent ?? "Select Agent")
                        .font(.caption2)
                        .fontWeight(.medium)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(Color.accentColor.opacity(0.15))
                        .foregroundStyle(Color.accentColor)
                        .cornerRadius(10)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(Color.accentColor.opacity(0.08))
            .cornerRadius(8)
            .padding(.horizontal, 12)
            .padding(.top, 8)

            Divider()
                .padding(.top, 12)

            // Session list
            SessionListView()
                .environmentObject(appState.sessionsViewModel)

            Divider()

            // Bottom navigation
            NavigationFooterView()
        }
        .frame(minWidth: 220, idealWidth: 260)
        .background(
            LinearGradient(
                colors: [
                    Color(red: 0.99, green: 0.95, blue: 0.92),  // warm peach top
                    Color(red: 0.98, green: 0.93, blue: 0.90),  // slightly deeper
                    Color(red: 0.97, green: 0.92, blue: 0.89),  // peachy bottom
                ],
                startPoint: .top,
                endPoint: .bottom
            )
        )
    }
}
