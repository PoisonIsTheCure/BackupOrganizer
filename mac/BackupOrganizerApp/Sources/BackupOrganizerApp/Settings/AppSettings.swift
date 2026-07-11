import Foundation

/// User-editable settings, backed by UserDefaults. The CLI path default
/// matches this machine's existing `bin/backup-organizer` shim (the same
/// path already hardcoded in com.alyz.backuporganizer.plist and Backup
/// Status.applescript) but is overridable here — unlike those two, since
/// this app is meant to eventually be usable on someone else's machine.
@MainActor
final class AppSettings: ObservableObject {
    static let shared = AppSettings()

    private enum Key {
        static let cliPath = "cliPath"
        static let configPathOverride = "configPathOverride"
    }

    static let defaultCLIPath =
        "/Users/alyz/DevProjects/Python/Tools/BackupOrganizer/bin/backup-organizer"

    @Published var cliPath: String {
        didSet { UserDefaults.standard.set(cliPath, forKey: Key.cliPath) }
    }

    /// Empty means "use the CLI's own default (~/Backups/BackupOrganizer/config.json)".
    @Published var configPathOverride: String? {
        didSet { UserDefaults.standard.set(configPathOverride, forKey: Key.configPathOverride) }
    }

    private init() {
        cliPath = UserDefaults.standard.string(forKey: Key.cliPath) ?? Self.defaultCLIPath
        configPathOverride = UserDefaults.standard.string(forKey: Key.configPathOverride)
    }
}
