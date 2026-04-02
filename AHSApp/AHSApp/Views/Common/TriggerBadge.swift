import SwiftUI

struct TriggerBadge: View {
    let trigger: TriggerType

    var body: some View {
        HStack(spacing: 2) {
            Image(systemName: trigger.systemImage)
                .font(.system(size: 7))
            Text(trigger.label)
                .font(.system(size: 8, weight: .medium))
        }
        .padding(.horizontal, 4)
        .padding(.vertical, 1)
        .background(backgroundColor.opacity(0.15))
        .foregroundStyle(backgroundColor)
        .cornerRadius(3)
    }

    private var backgroundColor: Color {
        switch trigger {
        case .api: .blue
        case .slack: .purple
        case .webhook: .orange
        case .cron: .teal
        }
    }
}
