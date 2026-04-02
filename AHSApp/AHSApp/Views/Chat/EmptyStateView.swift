import SwiftUI

struct EmptyStateView: View {
    @EnvironmentObject var appState: AppState

    let suggestions = [
        ("Fix a bug", "ladybug"),
        ("Deploy", "arrow.up.to.line"),
        ("Review PR", "arrow.triangle.pull"),
        ("Write code", "chevron.left.forwardslash.chevron.right"),
        ("Run SRE check", "shield.checkered"),
        ("Create project", "folder.badge.plus"),
    ]

    var body: some View {
        VStack(spacing: 24) {
            Spacer()

            // Logo
            Image(systemName: "cpu")
                .font(.system(size: 48))
                .foregroundStyle(.secondary)

            Text("Agent Harness Service")
                .font(.title2)
                .fontWeight(.semibold)

            Text("Select a session from the sidebar or create a new one")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            // Suggestion chips
            LazyVGrid(columns: [
                GridItem(.flexible()),
                GridItem(.flexible()),
                GridItem(.flexible()),
            ], spacing: 8) {
                ForEach(suggestions, id: \.0) { label, icon in
                    Button {
                        appState.showNewSessionPicker = true
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: icon)
                                .font(.caption)
                            Text(label)
                                .font(.caption)
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .frame(maxWidth: .infinity)
                        .background(Color(nsColor: .controlBackgroundColor))
                        .cornerRadius(8)
                    }
                    .buttonStyle(.plain)
                }
            }
            .frame(maxWidth: 420)

            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }
}
