import SwiftUI

struct ModelBadge: View {
    let model: String

    var body: some View {
        Text(shortModel)
            .font(.system(size: 9, weight: .medium, design: .monospaced))
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(badgeColor.opacity(0.12))
            .foregroundStyle(badgeColor)
            .cornerRadius(4)
    }

    private var shortModel: String {
        model
    }

    private var badgeColor: Color {
        let m = model.lowercased()
        if m.contains("opus") { return .purple }
        if m.contains("sonnet") { return .blue }
        if m.contains("haiku") { return .teal }
        if m.contains("gpt") { return .green }
        return .secondary
    }
}
