import SwiftUI

/// Two independent histories, both aimed at "why did that fail":
/// - App Commands: every CLI invocation this app made this session, exactly
///   as run (argv included) — the only place a failure *before* Python even
///   starts (wrong CLI path, launch failure) is visible at all.
/// - Backend Log: the Python side's own rotating log (backup_organizer.log),
///   which has history from outside this app too (launchd runs, Terminal
///   use) but only ever what cmd_backup et al. chose to log.info/log.error.
struct ActivityView: View {
    let client: BackendClient
    @ObservedObject private var activityLog = ActivityLog.shared
    @State private var logTailState: LoadState<LogTail> = .loading

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    appCommandsSection
                    Divider()
                    backendLogSection
                }
                .padding()
            }
        }
        .task { await loadLogTail() }
    }

    private var header: some View {
        HStack {
            Text("Activity").font(.title2.bold())
            Spacer()
            Button(role: .destructive) {
                activityLog.clear()
            } label: {
                Label("Clear", systemImage: "trash")
            }
            .disabled(activityLog.records.isEmpty)
            Button {
                Task { await loadLogTail() }
            } label: {
                Label("Refresh Log", systemImage: "arrow.clockwise")
            }
        }
        .padding()
    }

    // MARK: - App Commands

    private var appCommandsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("App Commands (\(activityLog.records.count))")
                .font(.headline)
            Text("Every CLI call this app has made since it launched, oldest failures first to spot.")
                .font(.caption).foregroundStyle(.secondary)
            if activityLog.records.isEmpty {
                Text("Nothing run yet.").foregroundStyle(.secondary).padding(.vertical, 4)
            } else {
                ForEach(activityLog.records) { record in
                    CommandRecordRow(record: record)
                }
            }
        }
    }

    // MARK: - Backend Log

    @ViewBuilder
    private var backendLogSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Backend Log").font(.headline)
            switch logTailState {
            case .loading:
                ProgressView()
            case .failed(let message):
                ErrorBanner(message: message) { Task { await loadLogTail() } }
            case .loaded(let tail):
                Text(tail.path).font(.caption).foregroundStyle(.secondary)
                if tail.lines.isEmpty {
                    Text("Log is empty.").foregroundStyle(.secondary)
                } else {
                    ScrollView(.horizontal) {
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(Array(tail.lines.enumerated()), id: \.offset) { _, line in
                                Text(line)
                                    .font(.caption.monospaced())
                                    .foregroundStyle(line.contains(" ERROR ") || line.contains(" WARNING ")
                                                     ? .orange : .primary)
                                    .lineLimit(1)
                            }
                        }
                    }
                    .frame(maxHeight: 320)
                    .padding(8)
                    .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
                }
            }
        }
    }

    private func loadLogTail() async {
        logTailState = .loading
        do {
            logTailState = .loaded(try await client.logTail(lines: 300))
        } catch {
            logTailState = .failed(error.localizedDescription)
        }
    }
}

private struct CommandRecordRow: View {
    let record: CommandRecord
    @State private var expanded = false

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 6) {
                if let launchError = record.launchError {
                    Label(launchError, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red).font(.caption)
                }
                if !record.stdout.isEmpty {
                    Text("stdout").font(.caption.bold()).foregroundStyle(.secondary)
                    Text(record.stdout).font(.caption.monospaced()).textSelection(.enabled)
                }
                if !record.stderr.isEmpty {
                    Text("stderr").font(.caption.bold()).foregroundStyle(.secondary)
                    Text(record.stderr).font(.caption.monospaced())
                        .foregroundStyle(.red).textSelection(.enabled)
                }
            }
            .padding(.top, 4)
        } label: {
            HStack {
                Image(systemName: record.succeeded ? "checkmark.circle.fill" : "xmark.octagon.fill")
                    .foregroundStyle(record.succeeded ? .green : .red)
                VStack(alignment: .leading, spacing: 2) {
                    Text(record.command).font(.caption.monospaced()).lineLimit(1)
                    Text(record.timestamp, style: .time)
                        .font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
                if let exitCode = record.exitCode {
                    Text("exit \(exitCode)").font(.caption2.monospaced()).foregroundStyle(.secondary)
                }
            }
        }
    }
}
