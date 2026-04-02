import SwiftUI

enum DetailTab: String, CaseIterable {
    case activity = "Activity"
    case files = "Files"
    case terminal = "Terminal"

    var systemImage: String {
        switch self {
        case .activity: "clock"
        case .files: "doc"
        case .terminal: "terminal"
        }
    }
}

struct DetailPanelView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var chatViewModel: ChatViewModel
    @State private var selectedTab: DetailTab = .activity

    var body: some View {
        VStack(spacing: 0) {
            // Tab bar
            HStack(spacing: 0) {
                ForEach(DetailTab.allCases, id: \.self) { tab in
                    Button {
                        selectedTab = tab
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: tab.systemImage)
                                .font(.caption2)
                            Text(tab.rawValue)
                                .font(.caption)
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(selectedTab == tab ? Color.accentColor.opacity(0.1) : Color.clear)
                        .foregroundStyle(selectedTab == tab ? Color.accentColor : .secondary)
                        .cornerRadius(6)
                    }
                    .buttonStyle(.plain)
                }
                Spacer()

                Button {
                    appState.showDetailPanel = false
                } label: {
                    Image(systemName: "xmark")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)

            Divider()

            // Tab content
            switch selectedTab {
            case .activity:
                ActivityTimelineView(activities: chatViewModel.toolActivities)
            case .files:
                FileChangesView(activities: chatViewModel.toolActivities)
            case .terminal:
                TerminalOutputView(activities: chatViewModel.toolActivities)
            }
        }
        .frame(minWidth: 280, idealWidth: 320, maxWidth: 380)
        .background(Color(nsColor: .controlBackgroundColor))
    }
}
