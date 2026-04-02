import Foundation

enum DateFormatting {
    nonisolated(unsafe) static let relative: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        return f
    }()

    static let shortDateTime: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .short
        f.timeStyle = .short
        return f
    }()

    static let timeOnly: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .none
        f.timeStyle = .short
        return f
    }()

    static func relativeString(from isoString: String?) -> String {
        guard let str = isoString,
              let date = ISO8601DateFormatter.flexible.date(from: str) else { return "" }
        return relative.localizedString(for: date, relativeTo: Date())
    }

    static func shortString(from isoString: String?) -> String {
        guard let str = isoString,
              let date = ISO8601DateFormatter.flexible.date(from: str) else { return "" }
        return shortDateTime.string(from: date)
    }
}
