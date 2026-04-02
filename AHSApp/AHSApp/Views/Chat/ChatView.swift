import SwiftUI

struct ChatView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var viewModel: ChatViewModel

    var body: some View {
        VStack(spacing: 0) {
            // Session header
            if viewModel.sessionId != nil {
                SessionHeaderView(
                    session: viewModel.sessionInfo,
                    agent: viewModel.agentInfo
                )
            }

            // Chat area or empty state
            if viewModel.sessionId == nil {
                EmptyStateView()
            } else {
                chatContent
            }

            // Input area
            if viewModel.sessionId != nil {
                MessageInputView(
                    isAgentThinking: viewModel.isAgentThinking,
                    onSend: { text in viewModel.sendMessage(text) },
                    onStop: { viewModel.stopAgent() }
                )
            }
        }
        .background(Color(nsColor: .textBackgroundColor))
    }

    @ViewBuilder
    private var chatContent: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(viewModel.messages.enumerated()), id: \.element.id) { index, message in
                        MessageView(message: message)
                            .id(message.id)

                        // Show turn summary after assistant messages
                        if message.role == .assistant,
                           let summaryIndex = turnSummaryIndex(for: index),
                           summaryIndex < viewModel.turnSummaries.count {
                            TurnSummaryRow(summary: viewModel.turnSummaries[summaryIndex])
                        }
                    }

                    // Tool activity for current turn
                    if !viewModel.currentTurnTools.isEmpty {
                        ToolActivityView(activities: viewModel.currentTurnTools)
                            .padding(.horizontal, 20)
                            .padding(.vertical, 8)
                    }

                    // Streaming text
                    if !viewModel.streamingText.isEmpty {
                        MessageView(message: ChatMessage(role: .assistant, content: viewModel.streamingText))
                            .id("streaming")
                    }

                    // Thinking indicator
                    if viewModel.isAgentThinking && viewModel.streamingText.isEmpty {
                        ThinkingIndicator()
                            .id("thinking")
                    }

                    // Error
                    if let error = viewModel.error {
                        ErrorBanner(message: error)
                            .padding(.horizontal, 20)
                            .padding(.vertical, 8)
                    }

                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.vertical, 12)
            }
            .onChange(of: viewModel.messages.count) { _, _ in
                withAnimation(.easeOut(duration: 0.2)) {
                    proxy.scrollTo("bottom", anchor: .bottom)
                }
            }
            .onChange(of: viewModel.streamingText) { _, _ in
                proxy.scrollTo("bottom", anchor: .bottom)
            }
        }
    }

    private func turnSummaryIndex(for messageIndex: Int) -> Int? {
        // Count assistant messages up to this index
        var count = 0
        for i in 0...messageIndex {
            if viewModel.messages[i].role == .assistant {
                count += 1
            }
        }
        return count - 1
    }
}

// MARK: - Turn Summary Row

struct TurnSummaryRow: View {
    let summary: TurnSummary

    var body: some View {
        HStack {
            Spacer()
            Text("\(summary.formattedTokens) \u{2022} \(summary.formattedCost) \u{2022} \(summary.formattedDuration)")
                .font(.caption2)
                .foregroundStyle(.tertiary)
            Spacer()
        }
        .padding(.vertical, 4)
    }
}

// MARK: - Thinking Indicator

struct ThinkingIndicator: View {
    @State private var dotCount = 0
    let timer = Timer.publish(every: 0.5, on: .main, in: .common).autoconnect()

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "cpu")
                .foregroundStyle(.secondary)
                .font(.caption)

            Text("Thinking" + String(repeating: ".", count: dotCount % 4))
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 8)
        .onReceive(timer) { _ in
            dotCount += 1
        }
    }
}

// MARK: - Error Banner

struct ErrorBanner: View {
    let message: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.red)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding(10)
        .background(Color.red.opacity(0.08))
        .cornerRadius(8)
    }
}
