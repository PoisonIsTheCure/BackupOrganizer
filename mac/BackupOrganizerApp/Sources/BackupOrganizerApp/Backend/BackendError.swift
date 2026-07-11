import Foundation

/// Everything that can go wrong shelling out to the backup-organizer CLI.
enum BackendError: Error, LocalizedError {
    case cliNotFound(String)
    case launchFailed(String)
    case nonZeroExit(code: Int32, stderr: String)
    case emptyOutput
    case decodingFailed(String)

    var errorDescription: String? {
        switch self {
        case .cliNotFound(let path):
            return "backup-organizer not found at \(path). Set the CLI path in Settings."
        case .launchFailed(let message):
            return "Could not launch backup-organizer: \(message)"
        case .nonZeroExit(let code, let stderr):
            let detail = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            return "backup-organizer exited with code \(code)\(detail.isEmpty ? "" : ": \(detail)")"
        case .emptyOutput:
            return "backup-organizer produced no output."
        case .decodingFailed(let message):
            return "Could not parse backup-organizer's JSON output: \(message)"
        }
    }
}
