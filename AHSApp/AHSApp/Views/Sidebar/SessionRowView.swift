import SwiftUI

struct SessionRowView: View {
    let session: Session
    let isSelected: Bool

    var body: some View {
        HStack(spacing: 8) {
            // Status dot
            StatusDot(status: session.status ?? .active, size: 8)

            VStack(alignment: .leading, spacing: 2) {
                Text(session.displayTitle)
                    .font(.subheadline)
                    .fontWeight(isSelected ? .semibold : .regular)
                    .lineLimit(1)

                HStack(spacing: 4) {
                    Text(session.displayAgent)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)

                    if let trigger = session.trigger {
                        TriggerBadge(trigger: trigger)
                    }
                }
            }

            Spacer()

            Text(session.timeAgo)
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(isSelected ? Color.accentColor.opacity(0.12) : Color.clear)
        .overlay(alignment: .leading) {
            if isSelected {
                Rectangle()
                    .fill(Color.accentColor)
                    .frame(width: 3)
            }
        }
        .contentShape(Rectangle())
        .cornerRadius(6)
        .padding(.horizontal, 4)
    }
}
