import SwiftUI

/// Cached state for a single session — preserved when switching between sessions.
struct SessionCache {
    var messages: [ChatMessage] = []
    var toolActivities: [ToolActivity] = []
    var turnSummaries: [TurnSummary] = []
    var sessionInfo: Session?
    var agentInfo: Agent?
    var historyLoaded = false
}

@MainActor
final class ChatViewModel: ObservableObject {
    @Published var messages: [ChatMessage] = []
    @Published var toolActivities: [ToolActivity] = []
    @Published var currentTurnTools: [ToolActivity] = []
    @Published var isAgentThinking = false
    @Published var streamingText = ""
    @Published var sessionInfo: Session?
    @Published var agentInfo: Agent?
    @Published var turnSummaries: [TurnSummary] = []
    @Published var error: String?

    private weak var appState: AppState?
    private var currentSessionId: String?

    /// Per-session cache: session_id -> cached state
    private var sessionCaches: [String: SessionCache] = [:]

    init(appState: AppState) {
        self.appState = appState
    }

    var sessionId: String? { currentSessionId }

    // MARK: - Session Management

    func attachSession(_ sessionId: String) {
        guard sessionId != currentSessionId else { return }

        // Save current session state to cache
        if let oldId = currentSessionId {
            saveToCache(oldId)
        }

        // Cancel title polling and disconnect old WebSocket
        titlePollTask?.cancel()
        appState?.webSocketClient.disconnect()

        currentSessionId = sessionId
        streamingText = ""
        isAgentThinking = false
        currentTurnTools = []
        error = nil

        // Restore from cache if available
        if let cached = sessionCaches[sessionId] {
            messages = cached.messages
            toolActivities = cached.toolActivities
            turnSummaries = cached.turnSummaries
            sessionInfo = cached.sessionInfo
            agentInfo = cached.agentInfo

            // Reconnect WebSocket for active sessions
            connectWebSocket()

            // If history was never loaded (shouldn't happen), load it
            if !cached.historyLoaded {
                Task {
                    await loadSessionInfo()
                    await loadHistory()
                }
            }
        } else {
            // Fresh load
            messages = []
            toolActivities = []
            turnSummaries = []
            sessionInfo = nil
            agentInfo = nil

            Task {
                await loadSessionInfo()
                await loadHistory()
                connectWebSocket()
            }
        }
    }

    private func saveToCache(_ sessionId: String) {
        var cache = sessionCaches[sessionId] ?? SessionCache()
        cache.messages = messages
        cache.toolActivities = toolActivities
        cache.turnSummaries = turnSummaries
        cache.sessionInfo = sessionInfo
        cache.agentInfo = agentInfo
        cache.historyLoaded = true
        sessionCaches[sessionId] = cache
    }

    private func loadSessionInfo() async {
        guard let sessionId = currentSessionId, let appState else { return }
        do {
            sessionInfo = try await appState.apiClient.getSession(sessionId)
        } catch {
            // Non-fatal
        }
        let agentName = sessionInfo?.agentName ?? sessionInfo?.agentId
        if let agentName {
            do {
                agentInfo = try await appState.apiClient.getAgent(agentName)
            } catch {
                // Non-fatal
            }
        }
        // Update cache
        if let sessionId = currentSessionId {
            sessionCaches[sessionId, default: SessionCache()].sessionInfo = sessionInfo
            sessionCaches[sessionId, default: SessionCache()].agentInfo = agentInfo
        }

    }

    private var titlePollTask: Task<Void, Never>?

    /// Start polling for session title after first turn completes. Checks at 5s, 10s, 15s, 20s.
    private func startTitlePolling(sessionId: String) {
        // Cancel any existing poll
        titlePollTask?.cancel()
        titlePollTask = Task {
            let delays: [UInt64] = [5, 5, 5, 5] // cumulative: 5s, 10s, 15s, 20s
            for delay in delays {
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                guard !Task.isCancelled, currentSessionId == sessionId, let appState else { return }
                do {
                    let updated = try await appState.apiClient.getSession(sessionId)
                    if let title = updated.title, !title.isEmpty {
                        self.sessionInfo = updated
                        sessionCaches[sessionId, default: SessionCache()].sessionInfo = updated
                        await appState.sessionsViewModel.refresh()
                        return
                    }
                } catch {
                    // Non-fatal
                }
            }
        }
    }

    private func loadHistory() async {
        guard let sessionId = currentSessionId, let appState else { return }
        do {
            let history = try await appState.apiClient.getHistory(sessionId: sessionId)
            // Only set if we're still on this session
            if currentSessionId == sessionId {
                self.messages = history
                // Save to cache
                sessionCaches[sessionId, default: SessionCache()].messages = history
                sessionCaches[sessionId, default: SessionCache()].historyLoaded = true
            }
        } catch {
            // Non-fatal — new sessions have no history
        }
    }

    private func connectWebSocket() {
        guard let sessionId = currentSessionId, let appState else { return }
        appState.webSocketClient.connect(sessionId: sessionId) { [weak self] event in
            Task { @MainActor in
                self?.handleWSEvent(event)
            }
        }
    }

    // MARK: - Send Message

    func sendMessage(_ text: String) {
        guard let appState, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }

        let userMessage = ChatMessage(role: .user, content: text)
        messages.append(userMessage)

        appState.webSocketClient.sendUserMessage(text, userId: appState.authService.userId)
    }

    func stopAgent() {
        appState?.webSocketClient.sendStop()
    }

    // MARK: - WebSocket Events

    private func handleWSEvent(_ event: WSEvent) {
        switch event.type {
        case "turn/started":
            isAgentThinking = true
            streamingText = ""
            currentTurnTools = []

        case "item/started":
            if let item = event.item {
                let toolName = item.tool ?? item.type ?? "unknown"
                let argStr: String?
                if let cmd = item.command, !cmd.isEmpty {
                    argStr = cmd
                } else if let args = item.arguments, !args.isEmpty {
                    argStr = args.compactMap { k, v in v.stringValue.map { "\(k): \($0)" } }.joined(separator: ", ")
                } else if let changes = item.changes, !changes.isEmpty {
                    argStr = changes.compactMap(\.path).joined(separator: ", ")
                } else {
                    argStr = nil
                }
                let activity = ToolActivity(
                    name: toolName,
                    server: item.server,
                    arguments: argStr,
                    status: .running,
                    startTime: Date()
                )
                currentTurnTools.append(activity)
                toolActivities.append(activity)
            }

        case "item/agentMessage/delta":
            if let delta = event.delta {
                streamingText += delta
            }

        case "item/completed":
            if let item = event.item, let lastIdx = currentTurnTools.indices.last(where: { currentTurnTools[$0].status == .running }) {
                let elapsed = Date().timeIntervalSince(currentTurnTools[lastIdx].startTime)
                currentTurnTools[lastIdx].duration = elapsed
                currentTurnTools[lastIdx].status = item.status == "failed" ? .failed : .completed

                if let mainIdx = toolActivities.indices.last(where: { toolActivities[$0].status == .running }) {
                    toolActivities[mainIdx].duration = elapsed
                    toolActivities[mainIdx].status = item.status == "failed" ? .failed : .completed
                }
            }

        case "turn/completed":
            isAgentThinking = false
            if !streamingText.isEmpty {
                let msg = ChatMessage(role: .assistant, content: streamingText)
                messages.append(msg)
                streamingText = ""
            }

            if let usage = event.usage {
                let summary = TurnSummary(
                    inputTokens: usage.inputTokens ?? 0,
                    outputTokens: usage.outputTokens ?? 0,
                    costUsd: usage.costUsd ?? 0,
                    durationMs: usage.durationMs ?? 0
                )
                turnSummaries.append(summary)
            }

            currentTurnTools = []

            // Auto-save to cache after each turn
            if let sid = currentSessionId {
                saveToCache(sid)
                // Poll for title after first turn if we don't have one yet
                if sessionInfo?.title == nil {
                    startTitlePolling(sessionId: sid)
                }
            }

        case "error":
            isAgentThinking = false
            if let errVal = event.error?.stringValue {
                error = errVal
            }

        default:
            break
        }
    }
}
