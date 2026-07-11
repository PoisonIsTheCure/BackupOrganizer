import Foundation

/// Every fact and action in this app goes through the Python
/// backup-organizer CLI via `--json` output — no business logic lives here,
/// no direct reads of config.json/manifest.json. See docs/ARCHITECTURE.md
/// in the main repo for why (the CLI already owns diffing, chunking, and
/// every Proton Drive call; duplicating that in Swift would just be a
/// second, divergent implementation of the same rules).
actor BackendClient {
    private let settings: AppSettings

    /// No default argument: `AppSettings.shared` is @MainActor-isolated,
    /// and this actor's init runs in a nonisolated context, so the caller
    /// (already on the main actor, e.g. a SwiftUI View) must pass it in.
    init(settings: AppSettings) {
        self.settings = settings
    }

    // MARK: - Process plumbing

    /// Runs the CLI with `args`, returns raw stdout. Throws on launch
    /// failure or a non-zero exit with no usable stdout at all.
    private func run(_ args: [String]) async throws -> (stdout: Data, stderr: String, exitCode: Int32) {
        let cliPath = await settings.cliPath
        guard FileManager.default.isExecutableFile(atPath: cliPath) else {
            throw BackendError.cliNotFound(cliPath)
        }
        let configPath = await settings.configPathOverride

        let process = Process()
        process.executableURL = URL(fileURLWithPath: cliPath)
        var fullArgs = args
        if let configPath, !configPath.isEmpty {
            fullArgs = ["--config", configPath] + fullArgs
        }
        process.arguments = fullArgs

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        do {
            try process.run()
        } catch {
            throw BackendError.launchFailed(error.localizedDescription)
        }

        let stdoutData = try stdoutPipe.fileHandleForReading.readToEndCompat()
        let stderrData = try stderrPipe.fileHandleForReading.readToEndCompat()
        process.waitUntilExit()

        let stderrText = String(data: stderrData, encoding: .utf8) ?? ""
        return (stdoutData, stderrText, process.terminationStatus)
    }

    /// Runs the CLI and decodes its stdout as JSON of type T. A non-zero
    /// exit is only fatal if stdout didn't contain decodable JSON — several
    /// commands (delete-remote, retire-sync-twin) legitimately exit 1 while
    /// still returning a JSON body describing partial success.
    private func runJSON<T: Decodable>(_ args: [String]) async throws -> T {
        let (stdout, stderr, exitCode) = try await run(args)
        guard !stdout.isEmpty else {
            if exitCode != 0 {
                throw BackendError.nonZeroExit(code: exitCode, stderr: stderr)
            }
            throw BackendError.emptyOutput
        }
        // Some commands (e.g. --list with no matches) can print a trailing
        // human line even in --json mode's error paths; only decode the
        // last non-empty line, which every JSON command writes exactly once.
        let text = String(data: stdout, encoding: .utf8) ?? ""
        let lastLine = text.split(separator: "\n").last.map(String.init) ?? text
        do {
            return try JSONDecoder().decode(T.self, from: Data(lastLine.utf8))
        } catch {
            if exitCode != 0 {
                throw BackendError.nonZeroExit(code: exitCode, stderr: stderr)
            }
            throw BackendError.decodingFailed(error.localizedDescription)
        }
    }

    // MARK: - Read-only

    func status() async throws -> StatusInfo {
        try await runJSON(["--status", "--json", "--no-notify"])
    }

    func list(kind: String) async throws -> [FileEntry] {
        try await runJSON(["--list", kind, "--json", "--no-notify"])
    }

    func orphans() async throws -> [OrphanEntry] {
        try await runJSON(["--orphans", "--json", "--no-notify"])
    }

    // MARK: - Mutating (buttons stay disabled in the read-only skeleton;
    // wired up once the interactive flows land)

    func archive(paths: [String], run runCycle: Bool = true) async throws -> ArchiveResult {
        var args = ["archive"] + paths
        if !runCycle { args.append("--no-run") }
        args += ["--json", "--no-notify"]
        return try await runJSON(args)
    }

    func addSync(dirs: [String], run runCycle: Bool = true) async throws -> AddSyncResult {
        var args = ["add-sync"] + dirs
        if !runCycle { args.append("--no-run") }
        args += ["--json", "--no-notify"]
        return try await runJSON(args)
    }

    func deleteRemote(arcnames: [String]) async throws -> DeleteRemoteResult {
        try await runJSON(["delete-remote"] + arcnames + ["--json", "--no-notify"])
    }

    func retireSyncTwin(arcnames: [String]) async throws -> RetireSyncTwinResult {
        try await runJSON(["retire-sync-twin"] + arcnames + ["--json", "--no-notify"])
    }
}

private extension FileHandle {
    /// `readDataToEndOfFile()` without the availability noise; isolated
    /// here so call sites read cleanly.
    func readToEndCompat() throws -> Data {
        if #available(macOS 10.15.4, *) {
            return (try? readToEnd()) ?? Data()
        }
        return readDataToEndOfFile()
    }
}
