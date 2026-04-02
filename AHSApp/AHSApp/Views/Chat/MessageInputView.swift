import SwiftUI

struct MessageInputView: View {
    let isAgentThinking: Bool
    let onSend: (String) -> Void
    let onStop: () -> Void

    @State private var text = ""
    @FocusState private var isFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            Divider()

            HStack(alignment: .bottom, spacing: 8) {
                // Attach button
                Button {
                    // Future: file picker
                } label: {
                    Image(systemName: "plus.circle")
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Attach file")

                // Text input using TextField for single-line alignment
                TextField("Type a message...", text: $text, axis: .vertical)
                    .font(.body)
                    .textFieldStyle(.plain)
                    .focused($isFocused)
                    .lineLimit(1...6)
                    .onKeyPress(.return, phases: .down) { keyPress in
                        if keyPress.modifiers.contains(.shift) {
                            return .ignored
                        }
                        send()
                        return .handled
                    }

                // Send or Stop button
                if isAgentThinking {
                    Button(action: onStop) {
                        Image(systemName: "stop.fill")
                            .font(.caption)
                            .frame(width: 28, height: 28)
                            .background(Color.red.opacity(0.8))
                            .foregroundStyle(.white)
                            .clipShape(Circle())
                    }
                    .buttonStyle(.plain)
                    .help("Stop agent (Esc)")
                } else {
                    Button(action: send) {
                        Image(systemName: "arrow.up")
                            .font(.caption.weight(.bold))
                            .frame(width: 28, height: 28)
                            .background(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? Color.secondary.opacity(0.3) : Color.accentColor)
                            .foregroundStyle(.white)
                            .clipShape(Circle())
                    }
                    .buttonStyle(.plain)
                    .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .help("Send message (Enter)")
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(Color(nsColor: .textBackgroundColor))

            // Keyboard shortcut hints
            HStack(spacing: 12) {
                Text("Enter to send")
                    .font(.caption2)
                    .foregroundStyle(.quaternary)
                Text("Shift+Enter for newline")
                    .font(.caption2)
                    .foregroundStyle(.quaternary)
                Spacer()
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 6)
        }
        .onAppear { isFocused = true }
    }

    private func send() {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        onSend(trimmed)
        text = ""
    }
}
