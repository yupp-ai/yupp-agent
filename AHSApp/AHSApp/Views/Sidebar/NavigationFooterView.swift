import SwiftUI

struct NavigationFooterView: View {
    @EnvironmentObject var appState: AppState

    var body: some View {
        VStack(spacing: 2) {
            NavRow(icon: "folder", label: "Projects", shortcut: "Cmd+1", destination: .projects)
            NavRow(icon: "clock", label: "Schedules", shortcut: "Cmd+2", destination: .schedules)
            NavRow(icon: "cpu", label: "Agents", shortcut: "Cmd+3", destination: .agents)
            NavRow(icon: "gearshape", label: "Settings", shortcut: "Cmd+,", destination: .settings)

            // User info
            if appState.authService.isAuthenticated {
                HStack(spacing: 6) {
                    Image(systemName: "person.circle.fill")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                    Text(appState.authService.userEmail)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                    Spacer()
                    Text(appState.selectedServer.rawValue)
                        .font(.system(size: 9))
                        .foregroundStyle(.quaternary)
                }
                .padding(.horizontal, 12)
                .padding(.top, 4)
                .padding(.bottom, 8)
            }
        }
        .padding(.horizontal, 4)
        .padding(.top, 4)
    }
}

struct NavRow: View {
    let icon: String
    let label: String
    let shortcut: String
    let destination: NavigationDestination

    @EnvironmentObject var appState: AppState

    var isActive: Bool { appState.currentView == destination }

    var body: some View {
        Button {
            appState.currentView = destination
        } label: {
            HStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.system(size: 13))
                    .frame(width: 20)
                    .foregroundStyle(isActive ? .primary : .secondary)
                Text(label)
                    .font(.system(size: 12))
                    .foregroundStyle(isActive ? .primary : .secondary)
                Spacer()
                Text(shortcut)
                    .font(.system(size: 10))
                    .foregroundStyle(.quaternary)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .contentShape(Rectangle())
            .background(isActive ? Color.accentColor.opacity(0.1) : Color.clear)
            .cornerRadius(6)
        }
        .buttonStyle(.plain)
    }
}
