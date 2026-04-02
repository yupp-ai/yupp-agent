import SwiftUI

struct KeyboardShortcutModifier: ViewModifier {
    @EnvironmentObject var appState: AppState

    func body(content: Content) -> some View {
        content
            .keyboardShortcut("k", modifiers: .command)  // handled separately
    }
}

// MARK: - App Commands

struct AppCommands: Commands {
    @ObservedObject var appState: AppState

    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button("New Session") {
                appState.showNewSessionPicker = true
            }
            .keyboardShortcut("n", modifiers: .command)
        }

        CommandMenu("Navigate") {
            Button("Chat") {
                appState.currentView = .chat
            }
            .keyboardShortcut(.return, modifiers: .command)

            Button("Projects") {
                appState.currentView = .projects
            }
            .keyboardShortcut("1", modifiers: .command)

            Button("Schedules") {
                appState.currentView = .schedules
            }
            .keyboardShortcut("2", modifiers: .command)

            Button("Agents") {
                appState.currentView = .agents
            }
            .keyboardShortcut("3", modifiers: .command)

            Divider()

            Button("Search") {
                appState.showSearchOverlay.toggle()
            }
            .keyboardShortcut("k", modifiers: .command)

            Button("Toggle Detail Panel") {
                appState.showDetailPanel.toggle()
            }
            .keyboardShortcut("d", modifiers: .command)

            Divider()

            Button("Toggle Sidebar") {
                withAnimation {
                    appState.sidebarVisible.toggle()
                }
            }
            .keyboardShortcut("s", modifiers: [.command, .control])
        }
    }
}
