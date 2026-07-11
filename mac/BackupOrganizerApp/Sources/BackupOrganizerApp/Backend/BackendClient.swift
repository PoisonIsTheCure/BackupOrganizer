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
    /// failure or a non-zero exit with no usable stdout at all. Every call
    /// — success or failure — is recorded to ActivityLog so the app always
    /// shows what it actually ran, matching the exact argv (config path
    /// included) that would reproduce it from a terminal.
    private func run(_ args: [String]) async throws -> (stdout: Data, stderr: String, exitCode: Int32) {
        let cliPath = await settings.cliPath
        let configPath = await settings.configPathOverride
        var fullArgs = args
        if let configPath, !configPath.isEmpty {
            fullArgs = ["--config", configPath] + fullArgs
        }
        let displayCommand = (["backup-organizer"] + fullArgs).joined(separator: " ")

        guard FileManager.default.isExecutableFile(atPath: cliPath) else {
            let message = "CLI not found or not executable at \(cliPath)"
            await ActivityLog.shared.record(command: displayCommand, exitCode: nil,
                                            stdout: "", stderr: "", launchError: message)
            throw BackendError.cliNotFound(cliPath)
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: cliPath)
        process.arguments = fullArgs

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        do {
            try process.run()
        } catch {
            await ActivityLog.shared.record(command: displayCommand, exitCode: nil,
                                            stdout: "", stderr: "",
                                            launchError: error.localizedDescription)
            throw BackendError.launchFailed(error.localizedDescription)
        }

        let stdoutData = try stdoutPipe.fileHandleForReading.readToEndCompat()
        let stderrData = try stderrPipe.fileHandleForReading.readToEndCompat()
        process.waitUntilExit()

        let stdoutText = String(data: stdoutData, encoding: .utf8) ?? "<non-UTF8 output>"
        let stderrText = String(data: stderrData, encoding: .utf8) ?? ""
        await ActivityLog.shared.record(command: displayCommand, exitCode: process.terminationStatus,
                                        stdout: stdoutText, stderr: stderrText)
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

    // NOTE: argparse subparsers own everything after the subcommand token,
    // so global flags like --json/--no-notify MUST precede the subcommand
    // (e.g. "--json archive PATH", never "archive PATH --json") — only
    // flags the subcommand itself defines (like archive's --no-run) may
    // follow it. Verified against the real CLI; getting this backwards
    // fails with argparse's "unrecognized arguments" for every mutating call.

    func archive(paths: [String], run runCycle: Bool = true) async throws -> ArchiveResult {
        var args = ["--json", "--no-notify", "archive"] + paths
        if !runCycle { args.append("--no-run") }
        return try await runJSON(args)
    }

    func addSync(dirs: [String], run runCycle: Bool = true) async throws -> AddSyncResult {
        var args = ["--json", "--no-notify", "add-sync"] + dirs
        if !runCycle { args.append("--no-run") }
        return try await runJSON(args)
    }

    func deleteRemote(arcnames: [String]) async throws -> DeleteRemoteResult {
        try await runJSON(["--json", "--no-notify", "delete-remote"] + arcnames)
    }

    func retireSyncTwin(arcnames: [String]) async throws -> RetireSyncTwinResult {
        try await runJSON(["--json", "--no-notify", "retire-sync-twin"] + arcnames)
    }

    /// Reconciles the local manifest against Proton Drive (cloud = ground
    /// truth) — recovers/fixes sync entries by cross-checking the remote
    /// listing against local files (no downloads), and confirms archive
    /// chunks are still present at the right size. See
    /// cmd_recalculate_manifest in commands.py. Can take a while on a large
    /// sync tree (one `filesystem list` call per remote folder).
    func recalculateManifest() async throws -> RecalculateManifestResult {
        try await runJSON(["--json", "--no-notify", "recalculate-manifest"])
    }

    /// Streams `run --json`'s NDJSON progress lines as they're printed —
    /// this is the only long-running command, so it's the only one that
    /// needs line-by-line streaming instead of run()'s buffer-then-decode.
    /// The stream always ends either with an `event == "result"` element
    /// (success or a reported failure) or a thrown error (the process
    /// exited without ever printing one — an unhandled crash on the Python
    /// side, since every reachable failure path in cmd_backup does emit
    /// "result" when json_out is set).
    func runBackup(dryRun: Bool = false, noUpload: Bool = false) -> AsyncThrowingStream<ProgressEvent, Error> {
        AsyncThrowingStream { continuation in
            Task {
                let cliPath = await settings.cliPath
                let configPath = await settings.configPathOverride
                var args = ["--json", "--no-notify"]
                if dryRun { args.append("--dry-run") }
                if noUpload { args.append("--no-upload") }
                if let configPath, !configPath.isEmpty {
                    args = ["--config", configPath] + args
                }
                args.append("run")
                let displayCommand = (["backup-organizer"] + args).joined(separator: " ")

                guard FileManager.default.isExecutableFile(atPath: cliPath) else {
                    let message = "CLI not found or not executable at \(cliPath)"
                    await ActivityLog.shared.record(command: displayCommand, exitCode: nil,
                                                    stdout: "", stderr: "", launchError: message)
                    continuation.finish(throwing: BackendError.cliNotFound(cliPath))
                    return
                }

                let process = Process()
                process.executableURL = URL(fileURLWithPath: cliPath)
                process.arguments = args

                let stdoutPipe = Pipe()
                let stderrPipe = Pipe()
                process.standardOutput = stdoutPipe
                process.standardError = stderrPipe

                var capturedLines: [String] = []
                do {
                    try process.run()

                    var sawResult = false
                    let decoder = JSONDecoder()
                    for try await line in stdoutPipe.fileHandleForReading.bytes.lines {
                        guard !line.isEmpty else { continue }
                        capturedLines.append(line)
                        guard let data = line.data(using: .utf8),
                              let event = try? decoder.decode(ProgressEvent.self, from: data) else { continue }
                        continuation.yield(event)
                        if event.isTerminal { sawResult = true }
                    }

                    let stderrData = try stderrPipe.fileHandleForReading.readToEndCompat()
                    process.waitUntilExit()
                    let stderrText = String(data: stderrData, encoding: .utf8) ?? ""

                    await ActivityLog.shared.record(command: displayCommand,
                                                    exitCode: process.terminationStatus,
                                                    stdout: capturedLines.joined(separator: "\n"),
                                                    stderr: stderrText)

                    if sawResult {
                        continuation.finish()
                    } else {
                        continuation.finish(throwing: BackendError.nonZeroExit(
                            code: process.terminationStatus, stderr: stderrText))
                    }
                } catch {
                    await ActivityLog.shared.record(command: displayCommand, exitCode: nil,
                                                    stdout: capturedLines.joined(separator: "\n"),
                                                    stderr: "", launchError: error.localizedDescription)
                    continuation.finish(throwing: error)
                }
            }
        }
    }

    /// Tail of backup_organizer.log — the Python side's own rotating log,
    /// which records history (past runs, exceptions) that ActivityLog
    /// (this app's own invocation history) doesn't have, since ActivityLog
    /// only exists from when the app was launched.
    func logTail(lines: Int = 200) async throws -> LogTail {
        try await runJSON(["--log-tail", String(lines), "--json", "--no-notify"])
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
