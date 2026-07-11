import Foundation

/// One CLI invocation the app made, kept for the Activity view so "what did
/// the app actually run, and did it fail" is always answerable without
/// leaving the app — this was the whole point of adding this: silent
/// failures (a crash before Python even starts, a JSON decode mismatch)
/// don't show up in backup_organizer.log at all, only here.
struct CommandRecord: Identifiable, Equatable {
    let id = UUID()
    let timestamp: Date
    let command: String
    let exitCode: Int32?
    let stdout: String
    let stderr: String
    let launchError: String?

    var succeeded: Bool { launchError == nil && (exitCode == nil || exitCode == 0) }
}

@MainActor
final class ActivityLog: ObservableObject {
    static let shared = ActivityLog()

    @Published private(set) var records: [CommandRecord] = []
    private let maxRecords = 300
    private let maxCapturedChars = 20_000 // avoid pinning huge NDJSON output in memory

    private init() {}

    func record(command: String, exitCode: Int32?, stdout: String, stderr: String,
               launchError: String? = nil) {
        let entry = CommandRecord(
            timestamp: Date(), command: command, exitCode: exitCode,
            stdout: String(stdout.suffix(maxCapturedChars)),
            stderr: String(stderr.suffix(maxCapturedChars)), launchError: launchError)
        records.insert(entry, at: 0)
        if records.count > maxRecords {
            records.removeLast(records.count - maxRecords)
        }
    }

    func clear() {
        records.removeAll()
    }
}
