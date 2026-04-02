import SwiftUI

struct StatusDot: View {
    let status: SessionStatus
    let size: CGFloat

    init(status: SessionStatus, size: CGFloat = 8) {
        self.status = status
        self.size = size
    }

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: size, height: size)
    }

    private var color: Color {
        switch status {
        case .active: .green
        case .completed: .gray
        case .stale: .orange
        }
    }
}
