import Foundation

@MainActor
final class WebSocketClient: ObservableObject {
    @Published var isConnected = false

    private var webSocketTask: URLSessionWebSocketTask?
    private let host: String
    private let apiKey: String
    private var onEvent: ((WSEvent) -> Void)?
    private var receiveTask: Task<Void, Never>?

    init(host: String = "ahs.yupp.ai", apiKey: String = "") {
        self.host = host
        self.apiKey = apiKey
    }

    func connect(sessionId: String, onEvent: @escaping (WSEvent) -> Void) {
        disconnect()
        self.onEvent = onEvent

        // Build WS URL matching the TUI: wss://host/ahs/session/{id}/ws?api_key=...
        let isLocal = host.hasPrefix("localhost") || host.hasPrefix("127.0.0.1") || host.hasPrefix("0.0.0.0")
        let scheme = isLocal ? "ws" : "wss"
        let hostWithPort: String
        if isLocal && !host.contains(":") {
            hostWithPort = "\(host):8090"
        } else {
            hostWithPort = host
        }
        let urlString = "\(scheme)://\(hostWithPort)/ahs/session/\(sessionId)/ws?api_key=\(apiKey)"
        guard let url = URL(string: urlString) else { return }

        let request = URLRequest(url: url)
        webSocketTask = URLSession.shared.webSocketTask(with: request)
        webSocketTask?.resume()
        isConnected = true

        receiveTask = Task { [weak self] in
            await self?.receiveLoop()
        }
    }

    func disconnect() {
        receiveTask?.cancel()
        receiveTask = nil
        webSocketTask?.cancel(with: .normalClosure, reason: nil)
        webSocketTask = nil
        isConnected = false
    }

    func sendUserMessage(_ content: String, userId: String) {
        let payload: [String: Any] = [
            "type": "user_message",
            "content": content,
            "user_id": userId,
            "source": "mac_app",
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let string = String(data: data, encoding: .utf8) else { return }
        webSocketTask?.send(.string(string)) { _ in }
    }

    func sendStop() {
        let payload = "{\"type\":\"stop\"}"
        webSocketTask?.send(.string(payload)) { _ in }
    }

    private func receiveLoop() async {
        guard let ws = webSocketTask else { return }
        while !Task.isCancelled {
            do {
                let message = try await ws.receive()
                switch message {
                case .string(let text):
                    handleMessage(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) {
                        handleMessage(text)
                    }
                @unknown default:
                    break
                }
            } catch {
                await MainActor.run {
                    self.isConnected = false
                }
                break
            }
        }
    }

    private func handleMessage(_ text: String) {
        // Skip heartbeat/pong messages
        if text == "pong" || text.contains("\"type\":\"pong\"") { return }

        guard let data = text.data(using: .utf8) else { return }
        do {
            let event = try JSONDecoder().decode(WSEvent.self, from: data)
            Task { @MainActor in
                self.onEvent?(event)
            }
        } catch {
            // Silently ignore unparseable messages
        }
    }
}
