import SwiftUI

struct CostLabel: View {
    let cost: Double

    var body: some View {
        Text(formattedCost)
            .font(.caption)
            .monospacedDigit()
            .foregroundStyle(.secondary)
    }

    private var formattedCost: String {
        if cost == 0 { return "" }
        if cost < 0.01 { return "<$0.01" }
        return String(format: "$%.2f", cost)
    }
}
