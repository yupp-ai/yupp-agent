import SwiftUI

@main
struct AHSApp: App {
    @StateObject private var appState = AppState()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appState)
                .environmentObject(appState.authService)
                .frame(minWidth: 800, minHeight: 500)
                .onAppear {
                    if appState.apiKey.isEmpty {
                        print("WARNING: AGENT_HARNESS_SERVICE_API_KEY not set. Set it and relaunch.")
                    }
                }
        }
        .windowStyle(.titleBar)
        .windowToolbarStyle(.unified(showsTitle: false))
        .commands {
            AppCommands(appState: appState)
        }
        .defaultSize(width: 1200, height: 800)
    }
}
