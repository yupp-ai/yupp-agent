import Foundation
import AuthenticationServices

/// Google OAuth configuration — matches the TUI's installed app credentials.
/// Per Google docs, the client_secret for installed/desktop apps is not confidential.
private enum GoogleOAuth {
    static let clientId = "451082535721-rt8inmimemumdhfm528ert2b37v09s8t.apps.googleusercontent.com"
    static let clientSecret = "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"
    static let authURI = "https://accounts.google.com/o/oauth2/auth"
    static let tokenURI = "https://oauth2.googleapis.com/token"
    static let userinfoURI = "https://www.googleapis.com/oauth2/v2/userinfo"
    static let scopes = "openid email"
    static let allowedDomain = "yupp.ai"
}

/// Stored Google OAuth tokens (compatible with TUI's ~/.config/ahstui/token.json format).
struct GoogleTokens: Codable {
    let token: String?
    let refreshToken: String?
    let tokenUri: String?
    let clientId: String?
    let clientSecret: String?
    let scopes: [String]?

    enum CodingKeys: String, CodingKey {
        case token
        case refreshToken = "refresh_token"
        case tokenUri = "token_uri"
        case clientId = "client_id"
        case clientSecret = "client_secret"
        case scopes
    }
}

@MainActor
final class AuthService: ObservableObject {
    @Published var isAuthenticated = false
    @Published var userEmail: String = ""
    @Published var userId: String = ""
    @Published var isLoading = false
    @Published var error: String?

    private let apiClient: APIClient

    // Share the TUI's credential cache
    private let configDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".config/ahstui")
    private var tokenPath: URL { configDir.appendingPathComponent("token.json") }
    private var emailCachePath: URL { configDir.appendingPathComponent("email.txt") }

    // Local server for OAuth redirect
    private var localServer: OAuthLocalServer?

    init(apiClient: APIClient) {
        self.apiClient = apiClient
        // Try to restore from TUI's cached credentials
        loadCachedCredentials()
    }

    // MARK: - Cached Credentials (shared with TUI)

    private func loadCachedCredentials() {
        // Check TUI's email cache first
        if let email = try? String(contentsOf: emailCachePath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
           email.hasSuffix("@\(GoogleOAuth.allowedDomain)"),
           let tokens = loadTokens(),
           tokens.token != nil {
            self.userEmail = email
            self.isAuthenticated = true
            // Resolve user ID in background
            Task { await resolveUserId(email: email) }
        }
    }

    private func loadTokens() -> GoogleTokens? {
        guard let data = try? Data(contentsOf: tokenPath) else { return nil }
        return try? JSONDecoder().decode(GoogleTokens.self, from: data)
    }

    private func saveCredentials(accessToken: String, refreshToken: String?, email: String) {
        try? FileManager.default.createDirectory(at: configDir, withIntermediateDirectories: true)

        let tokens = GoogleTokens(
            token: accessToken,
            refreshToken: refreshToken,
            tokenUri: GoogleOAuth.tokenURI,
            clientId: GoogleOAuth.clientId,
            clientSecret: GoogleOAuth.clientSecret,
            scopes: ["openid", "https://www.googleapis.com/auth/userinfo.email"]
        )
        if let data = try? JSONEncoder().encode(tokens) {
            try? data.write(to: tokenPath)
        }
        try? email.write(to: emailCachePath, atomically: true, encoding: .utf8)
    }

    // MARK: - Google OAuth Flow

    /// Starts the Google OAuth flow — opens the system browser, listens on a local port for the redirect.
    func signInWithGoogle() async {
        isLoading = true
        error = nil
        defer { isLoading = false }

        // First try to refresh existing token
        if let tokens = loadTokens(), let refreshToken = tokens.refreshToken {
            if let result = await refreshAccessToken(refreshToken) {
                await completeSignIn(accessToken: result.accessToken, refreshToken: refreshToken)
                return
            }
        }

        // Start local server and open browser
        do {
            let server = OAuthLocalServer()
            self.localServer = server
            let port = try server.start()

            let redirectURI = "http://localhost:\(port)"
            let authURL = "\(GoogleOAuth.authURI)?" + [
                "client_id=\(GoogleOAuth.clientId)",
                "redirect_uri=\(redirectURI)",
                "response_type=code",
                "scope=\(GoogleOAuth.scopes.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")",
                "access_type=offline",
                "prompt=consent",
            ].joined(separator: "&")

            // Open browser
            if let url = URL(string: authURL) {
                NSWorkspace.shared.open(url)
            }

            // Wait for the redirect with auth code
            let code = try await server.waitForCode()
            server.stop()
            self.localServer = nil

            // Exchange code for tokens
            guard let tokenResult = await exchangeCode(code, redirectURI: redirectURI) else {
                self.error = "Failed to exchange authorization code"
                return
            }

            await completeSignIn(accessToken: tokenResult.accessToken, refreshToken: tokenResult.refreshToken)
        } catch {
            self.error = "OAuth flow failed: \(error.localizedDescription)"
            localServer?.stop()
            localServer = nil
        }
    }

    private func completeSignIn(accessToken: String, refreshToken: String?) async {
        // Get email from Google userinfo
        guard let email = await fetchEmail(accessToken: accessToken) else {
            self.error = "Could not fetch email from Google"
            return
        }

        guard email.hasSuffix("@\(GoogleOAuth.allowedDomain)") else {
            self.error = "Only @\(GoogleOAuth.allowedDomain) accounts are allowed (got \(email))"
            return
        }

        saveCredentials(accessToken: accessToken, refreshToken: refreshToken, email: email)
        self.userEmail = email
        self.isAuthenticated = true
        await resolveUserId(email: email)
    }

    private func resolveUserId(email: String) async {
        do {
            let uid = try await apiClient.resolveUser(email: email)
            self.userId = uid
        } catch {
            // Non-fatal — we still have the email
            self.error = "Signed in but could not resolve user ID: \(error.localizedDescription)"
        }
    }

    // MARK: - Token Exchange

    private struct TokenResponse: Decodable {
        let accessToken: String
        let refreshToken: String?
        enum CodingKeys: String, CodingKey {
            case accessToken = "access_token"
            case refreshToken = "refresh_token"
        }
    }

    private func exchangeCode(_ code: String, redirectURI: String) async -> TokenResponse? {
        let body = [
            "code": code,
            "client_id": GoogleOAuth.clientId,
            "client_secret": GoogleOAuth.clientSecret,
            "redirect_uri": redirectURI,
            "grant_type": "authorization_code",
        ]
        return await tokenRequest(body: body)
    }

    private func refreshAccessToken(_ refreshToken: String) async -> TokenResponse? {
        let body = [
            "refresh_token": refreshToken,
            "client_id": GoogleOAuth.clientId,
            "client_secret": GoogleOAuth.clientSecret,
            "grant_type": "refresh_token",
        ]
        return await tokenRequest(body: body)
    }

    private func tokenRequest(body: [String: String]) async -> TokenResponse? {
        guard let url = URL(string: GoogleOAuth.tokenURI) else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        let bodyString = body.map { "\($0.key)=\($0.value.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")" }.joined(separator: "&")
        request.httpBody = bodyString.data(using: .utf8)

        guard let (data, _) = try? await URLSession.shared.data(for: request) else { return nil }
        return try? JSONDecoder().decode(TokenResponse.self, from: data)
    }

    // MARK: - Userinfo

    private func fetchEmail(accessToken: String) async -> String? {
        guard let url = URL(string: GoogleOAuth.userinfoURI) else { return nil }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")

        guard let (data, _) = try? await URLSession.shared.data(for: request) else { return nil }

        struct UserInfo: Decodable { let email: String? }
        return (try? JSONDecoder().decode(UserInfo.self, from: data))?.email?.lowercased()
    }

    // MARK: - Sign Out

    func signOut() {
        userEmail = ""
        userId = ""
        isAuthenticated = false
        error = nil
        try? FileManager.default.removeItem(at: tokenPath)
        try? FileManager.default.removeItem(at: emailCachePath)
    }
}

// MARK: - Local HTTP Server for OAuth Redirect

/// Minimal HTTP server that listens on a random port and captures the OAuth authorization code.
final class OAuthLocalServer: @unchecked Sendable {
    private var listener: (any NSObjectProtocol)?
    private var serverSocket: Int32 = -1
    private var port: UInt16 = 0
    private var codeContinuation: CheckedContinuation<String, Error>?
    private var listenThread: Thread?
    private var isRunning = false

    func start() throws -> UInt16 {
        serverSocket = socket(AF_INET, SOCK_STREAM, 0)
        guard serverSocket >= 0 else { throw OAuthError.serverFailed }

        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0  // Random port
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")

        var addrCopy = addr
        let bindResult = withUnsafePointer(to: &addrCopy) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPtr in
                bind(serverSocket, sockaddrPtr, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else { throw OAuthError.serverFailed }

        listen(serverSocket, 1)

        // Get the actual port
        var boundAddr = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &boundAddr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPtr in
                getsockname(serverSocket, sockaddrPtr, &len)
            }
        }
        port = UInt16(bigEndian: boundAddr.sin_port)
        isRunning = true

        return port
    }

    func waitForCode() async throws -> String {
        return try await withCheckedThrowingContinuation { continuation in
            self.codeContinuation = continuation

            // Accept connection on background thread
            DispatchQueue.global().async { [self] in
                let clientSocket = accept(self.serverSocket, nil, nil)
                guard clientSocket >= 0 else {
                    continuation.resume(throwing: OAuthError.serverFailed)
                    return
                }

                // Read request
                var buffer = [UInt8](repeating: 0, count: 4096)
                let bytesRead = recv(clientSocket, &buffer, buffer.count, 0)
                guard bytesRead > 0 else {
                    close(clientSocket)
                    continuation.resume(throwing: OAuthError.serverFailed)
                    return
                }

                let requestString = String(bytes: buffer[0..<bytesRead], encoding: .utf8) ?? ""

                // Extract code from query string: GET /?code=AUTH_CODE&scope=...
                var authCode: String?
                if let queryStart = requestString.range(of: "?"),
                   let lineEnd = requestString.range(of: " HTTP") {
                    let queryString = String(requestString[queryStart.upperBound..<lineEnd.lowerBound])
                    let params = queryString.split(separator: "&")
                    for param in params {
                        let kv = param.split(separator: "=", maxSplits: 1)
                        if kv.count == 2 && kv[0] == "code" {
                            authCode = String(kv[1]).removingPercentEncoding
                        }
                    }
                }

                // Send response
                let html: String
                if authCode != nil {
                    html = "<html><body><h2>Login successful!</h2><p>You can close this tab and return to AHS.</p></body></html>"
                } else {
                    html = "<html><body><h2>Login failed</h2><p>No authorization code received.</p></body></html>"
                }
                let response = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n\(html)"
                _ = response.withCString { ptr in
                    send(clientSocket, ptr, strlen(ptr), 0)
                }
                close(clientSocket)

                if let code = authCode {
                    continuation.resume(returning: code)
                } else {
                    continuation.resume(throwing: OAuthError.noCode)
                }
            }
        }
    }

    func stop() {
        isRunning = false
        if serverSocket >= 0 {
            close(serverSocket)
            serverSocket = -1
        }
    }
}

enum OAuthError: LocalizedError {
    case serverFailed
    case noCode
    case invalidEmail

    var errorDescription: String? {
        switch self {
        case .serverFailed: return "Failed to start OAuth server"
        case .noCode: return "No authorization code received"
        case .invalidEmail: return "Invalid email domain"
        }
    }
}
