import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var appState: AppState

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Settings")
                    .font(.title2)
                    .fontWeight(.semibold)
                Spacer()
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 12)

            Divider()

            Form {
                Section("Server") {
                    Picker("Environment", selection: Binding(
                        get: { appState.selectedServer },
                        set: { appState.switchServer($0) }
                    )) {
                        ForEach(ServerEnvironment.allCases) { server in
                            Text(server.label).tag(server)
                        }
                    }
                    .pickerStyle(.menu)

                    HStack {
                        Text("API Key")
                        Spacer()
                        if appState.apiKey.isEmpty {
                            Label("Not set", systemImage: "exclamationmark.triangle")
                                .font(.caption)
                                .foregroundStyle(.red)
                        } else {
                            Image(systemName: "checkmark.circle.fill")
                                .font(.caption)
                                .foregroundStyle(.green)
                        }
                    }
                }

                Section("Account") {
                    if appState.authService.isAuthenticated {
                        HStack {
                            Label(appState.authService.userEmail, systemImage: "person.circle.fill")
                            Spacer()
                            Button("Sign Out") {
                                appState.authService.signOut()
                            }
                            .foregroundStyle(.red)
                        }
                        if !appState.authService.userId.isEmpty {
                            HStack {
                                Text("User ID")
                                    .foregroundStyle(.secondary)
                                Spacer()
                                Text(appState.authService.userId)
                                    .font(.caption)
                                    .foregroundStyle(.tertiary)
                                    .textSelection(.enabled)
                            }
                        }
                    } else {
                        Button("Sign in with Google") {
                            Task { await appState.authService.signInWithGoogle() }
                        }
                        Text("Opens your browser for Google sign-in. Only @yupp.ai accounts are allowed.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                Section("About") {
                    HStack {
                        Text("Version")
                        Spacer()
                        Text("0.1.0")
                            .foregroundStyle(.secondary)
                    }
                    HStack {
                        Text("Data")
                        Spacer()
                        Text("~/.ahsapp/")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .formStyle(.grouped)

            Spacer()
        }
        .background(Color(nsColor: .textBackgroundColor))
    }
}

// MARK: - Sign-In / Startup Screen

struct SignInSheet: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.dismiss) var dismiss

    var body: some View {
        VStack(spacing: 24) {
            Spacer()

            Image(systemName: "cpu")
                .font(.system(size: 48))
                .foregroundStyle(.secondary)

            Text("Agent Harness Service")
                .font(.title2)
                .fontWeight(.semibold)

            // Server picker
            VStack(alignment: .leading, spacing: 6) {
                Text("Server")
                    .font(.caption)
                    .fontWeight(.medium)
                    .foregroundStyle(.secondary)

                Picker("Server", selection: Binding(
                    get: { appState.selectedServer },
                    set: { appState.switchServer($0) }
                )) {
                    ForEach(ServerEnvironment.allCases) { server in
                        Text(server.label).tag(server)
                    }
                }
                .pickerStyle(.menu)
                .frame(width: 300)
            }

            // API key status (subtle)
            if appState.apiKey.isEmpty {
                Label("API key required", systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                    .font(.subheadline)
            }

            // Sign in button
            Button {
                Task { await appState.authService.signInWithGoogle() }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "globe")
                    Text("Sign in with Google")
                }
                .frame(width: 220)
            }
            .controlSize(.large)
            .buttonStyle(.borderedProminent)
            .disabled(appState.authService.isLoading)

            if appState.authService.isLoading {
                HStack(spacing: 8) {
                    ProgressView()
                        .scaleEffect(0.7)
                    Text("Waiting for browser sign-in...")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            if let error = appState.authService.error {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 360)
            }

            Spacer()

            Text("Credentials shared with ahstui CLI")
                .font(.caption2)
                .foregroundStyle(.quaternary)
                .padding(.bottom, 16)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
        .onChange(of: appState.authService.isAuthenticated) { _, isAuth in
            if isAuth { dismiss() }
        }
    }
}
