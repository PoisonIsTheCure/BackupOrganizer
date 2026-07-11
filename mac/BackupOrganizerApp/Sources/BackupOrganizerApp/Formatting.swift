import Foundation

/// Byte formatting to match backuporganizer/util.py's human_size (same
/// units, same one-decimal style) so numbers read identically in the CLI
/// and the app.
func humanSize(_ bytes: Int) -> String {
    var n = Double(bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"] {
        if n < 1024 || unit == "TB" {
            return unit == "B" ? "\(Int(n)) B" : String(format: "%.1f %@", n, unit)
        }
        n /= 1024
    }
    return String(format: "%.1f TB", n)
}

/// Parses one of the ISO-8601-with-offset timestamps every backend command
/// emits (Python's `datetime.isoformat(timespec="seconds")`, e.g.
/// "2026-07-11T21:00:00+02:00") — shared so every view formats them the
/// same way instead of each re-parsing.
private let isoParser: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

private let relativeFormatter: RelativeDateTimeFormatter = {
    let f = RelativeDateTimeFormatter()
    f.unitsStyle = .full
    return f
}()

private let absoluteFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateStyle = .medium
    f.timeStyle = .short
    return f
}()

/// "" -> `emptyLabel`; a recent timestamp -> "3 minutes ago"; anything
/// older than a week -> an absolute date, since "47 days ago" stops being
/// useful information past a certain point.
func humanTimestamp(_ iso: String, emptyLabel: String = "never") -> String {
    guard !iso.isEmpty, let date = isoParser.date(from: iso) else {
        return iso.isEmpty ? emptyLabel : iso
    }
    let age = -date.timeIntervalSinceNow
    if age >= 0, age < 7 * 24 * 3600 {
        return relativeFormatter.localizedString(for: date, relativeTo: Date())
    }
    return absoluteFormatter.string(from: date)
}
