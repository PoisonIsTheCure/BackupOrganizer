import AppKit
import SwiftUI

/// Drag-to-select-then-⌘C is unreliable inside scrolling monospaced text in
/// SwiftUI (this is exactly what prompted adding these buttons — a user
/// couldn't get a failing command's output out of the app any other way).
/// An explicit button is the robust path; .textSelection(.enabled) stays on
/// as a secondary option for whoever prefers dragging.
func copyToClipboard(_ text: String) {
    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    pasteboard.setString(text, forType: .string)
}

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

    @State private var showRecalculateConfirm = false
    @State private var isRecalculating = false
    @State private var recalculateResult: RecalculateManifestResult?
    @State private var recalculateError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if isRecalculating || recalculateResult != nil {
                        recalculatePanel
                        Divider()
                    }
                    appCommandsSection
                    Divider()
                    backendLogSection
                }
                .padding()
            }
        }
        .task { await loadLogTail() }
        .confirmationDialog(
            "Recalculate the remote manifest?",
            isPresented: $showRecalculateConfirm
        ) {
            Button("Recalculate", role: .destructive) {
                Task { await recalculateManifest() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Cross-checks the synced-file manifest against what's actually on Proton Drive "
                + "and reconciles it: recovers entries the local manifest lost track of, resets "
                + "entries wrongly marked uploaded, and clears stale orphans. Archive chunks are "
                + "only checked for presence, not rebuilt. This only rewrites local bookkeeping "
                + "(manifest.json) — no files are uploaded, downloaded, or deleted.")
        }
        .alert("Recalculate failed", isPresented: Binding(
            get: { recalculateError != nil }, set: { if !$0 { recalculateError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(recalculateError ?? "")
        }
    }

    private var header: some View {
        HStack {
            Text("Activity").font(.title2.bold())
            Spacer()
            Button {
                showRecalculateConfirm = true
            } label: {
                Label("Recalculate Remote Manifest", systemImage: "arrow.triangle.2.circlepath.circle")
            }
            .disabled(isRecalculating)
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

    // MARK: - Recalculate

    @ViewBuilder
    private var recalculatePanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Remote Manifest Check").font(.headline)
            if isRecalculating {
                HStack {
                    ProgressView().controlSize(.small)
                    Text("Listing the remote tree and cross-checking against local files…")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } else if let result = recalculateResult {
                let s = result.sync
                VStack(alignment: .leading, spacing: 4) {
                    Text("Sync: \(s.remoteSyncFiles) file(s) on the cloud")
                        .font(.caption.bold())
                    recalculateRow("Recovered", s.newlyRecovered.count, .green)
                    recalculateRow("Confirmed pending → uploaded", s.confirmedPending.count, .green)
                    recalculateRow("Size mismatch (re-verified)", s.sizeMismatch.count, .orange)
                    recalculateRow("Found on cloud, no local match", s.unmatchedNoLocal.count, .orange)
                    recalculateRow("Reset to pending (not actually on cloud)", s.staleCleared.count, .orange)
                    recalculateRow("Stale orphans cleared", s.orphansCleared.count, .secondary)
                    Text("Archive: \(result.archive.confirmed.count) confirmed, "
                        + "\(result.archive.resetToPending.count) reset to pending")
                        .font(.caption.bold()).padding(.top, 4)
                }
            }
        }
    }

    private func recalculateRow(_ label: String, _ count: Int, _ color: Color) -> some View {
        HStack {
            Text(label).font(.caption)
            Spacer()
            Text("\(count)").font(.caption.monospacedDigit().bold())
                .foregroundStyle(count > 0 ? color : .secondary)
        }
    }

    private func recalculateManifest() async {
        isRecalculating = true
        recalculateResult = nil
        do {
            recalculateResult = try await client.recalculateManifest()
        } catch {
            recalculateError = error.localizedDescription
        }
        isRecalculating = false
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
            HStack {
                Text("Backend Log").font(.headline)
                Spacer()
                if case .loaded(let tail) = logTailState, !tail.lines.isEmpty {
                    Button {
                        copyToClipboard(tail.lines.joined(separator: "\n"))
                    } label: {
                        Label("Copy Log", systemImage: "doc.on.doc")
                    }
                    .font(.caption)
                }
            }
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
                    // Scrolling both axes, with each line forced to its
                    // natural (unwrapped) size: a horizontal-only ScrollView
                    // around a vertical stack of Text doesn't give the
                    // stack a real height to lay out against, so lines
                    // collapse and render on top of each other instead of
                    // stacking — .fixedSize + both-axis scrolling fixes it.
                    ScrollView([.horizontal, .vertical]) {
                        LazyVStack(alignment: .leading, spacing: 2) {
                            ForEach(Array(tail.lines.enumerated()), id: \.offset) { _, line in
                                Text(line)
                                    .font(.caption.monospaced())
                                    .foregroundStyle(line.contains(" ERROR ") || line.contains(" WARNING ")
                                                     ? .orange : .primary)
                                    .fixedSize(horizontal: true, vertical: false)
                                    .textSelection(.enabled)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
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

    private var copyText: String {
        var parts = ["$ \(record.command)"]
        if let exitCode = record.exitCode { parts.append("exit \(exitCode)") }
        if let launchError = record.launchError { parts.append("launch error: \(launchError)") }
        if !record.stdout.isEmpty { parts.append("stdout:\n\(record.stdout)") }
        if !record.stderr.isEmpty { parts.append("stderr:\n\(record.stderr)") }
        return parts.joined(separator: "\n\n")
    }

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
                Button {
                    copyToClipboard(copyText)
                } label: {
                    Label("Copy", systemImage: "doc.on.doc")
                }
                .font(.caption)
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
