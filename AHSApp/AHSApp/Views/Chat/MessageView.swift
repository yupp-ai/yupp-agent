import SwiftUI

struct MessageView: View {
    let message: ChatMessage
    @State private var webViewHeight: CGFloat = 40

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            if message.role == .user {
                Spacer(minLength: 80)
            }

            VStack(alignment: message.role == .user ? .trailing : .leading, spacing: 4) {
                // Avatar + role label
                HStack(spacing: 6) {
                    if message.role != .user {
                        Image(systemName: message.role == .assistant ? "cpu" : "person.circle")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text(message.role == .assistant ? "Agent" : message.role.rawValue.capitalized)
                            .font(.caption)
                            .fontWeight(.medium)
                            .foregroundStyle(.secondary)
                    } else {
                        Text("You")
                            .font(.caption)
                            .fontWeight(.medium)
                            .foregroundStyle(.secondary)
                        Image(systemName: "person.circle.fill")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                // Message content
                if message.role == .user {
                    Text(message.content)
                        .font(.body)
                        .textSelection(.enabled)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(messageBackground)
                        .cornerRadius(12)
                } else {
                    // Agent messages: full markdown, auto-sized to content
                    MarkdownWebView(markdown: message.content, dynamicHeight: $webViewHeight)
                        .frame(height: webViewHeight)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(messageBackground)
                        .cornerRadius(12)
                }

                // Tool uses — collapsible block
                if let tools = message.toolUses, !tools.isEmpty {
                    CollapsibleToolBlock(tools: tools)
                }
            }

            if message.role != .user {
                Spacer(minLength: 80)
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 4)
    }

    private var messageBackground: Color {
        switch message.role {
        case .user:
            return Color.accentColor.opacity(0.12)
        case .assistant, .agent:
            return Color(nsColor: .controlBackgroundColor)
        case .system:
            return Color.orange.opacity(0.08)
        }
    }
}

// MARK: - Collapsible Tool Block

struct CollapsibleToolBlock: View {
    let tools: [ToolUse]
    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header — always visible, click to expand
            Button {
                withAnimation(.easeInOut(duration: 0.15)) {
                    isExpanded.toggle()
                }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "wrench.and.screwdriver")
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                    Text("\(tools.count) tool\(tools.count == 1 ? "" : "s") used")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.system(size: 8))
                        .foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color(nsColor: .controlBackgroundColor).opacity(0.5))
                .cornerRadius(5)
            }
            .buttonStyle(.plain)

            if isExpanded {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(tools) { tool in
                        HStack(spacing: 4) {
                            Image(systemName: tool.status == "completed" ? "checkmark.circle.fill" : "circle.dotted.circle")
                                .font(.caption2)
                                .foregroundStyle(tool.status == "completed" ? .green : .orange)
                            Text(tool.displayName)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .padding(.horizontal, 8)
                .padding(.top, 4)
            }
        }
        .padding(.horizontal, 8)
    }
}
