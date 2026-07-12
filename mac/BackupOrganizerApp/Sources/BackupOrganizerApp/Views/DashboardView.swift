import SwiftUI

struct DashboardView: View {
    let client: BackendClient
    @State private var state: LoadState<StatusInfo> = .loading

    @State private var isRunning = false
    @State private var isPausing = false
    @State private var progressEvents: [ProgressEvent] = []
    @State private var runFailed = false
    @State private var runPaused = false
    @State private var runSummary: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            if isRunning || runSummary != nil {
                progressPanel
                Divider()
            }
            content
        }
        .task { await load() }
    }

    private var header: some View {
        HStack {
            Text("Dashboard").font(.title2.bold())
            Spacer()
            AddSyncButton(client: client) { Task { await load() } }
                .disabled(isRunning)
            ArchiveFilesButton(client: client) { Task { await load() } }
                .disabled(isRunning)
            Button {
                Task { await load() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .disabled(isRunning)
            if isRunning {
                Button {
                    Task { await pauseBackup() }
                } label: {
                    if isPausing {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Pause", systemImage: "pause.fill")
                    }
                }
                .disabled(isPausing)
                .help("Finishes the current file/chunk, then stops cleanly. "
                     + "Resume anytime by running the backup again.")
            }
            Button {
                Task { await runBackup() }
            } label: {
                if isRunning {
                    ProgressView().controlSize(.small)
                } else {
                    Label("Run Backup", systemImage: "play.fill")
                }
            }
            .disabled(isRunning)
        }
        .padding()
    }

    private var progressPanel: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let runSummary {
                Label(runSummary, systemImage: runFailed ? "xmark.octagon.fill"
                     : runPaused ? "pause.circle.fill" : "checkmark.circle.fill")
                    .foregroundStyle(runFailed ? .red : runPaused ? .orange : .green)
                    .font(.callout.bold())
            }
            ForEach(Array(progressEvents.suffix(6).enumerated()), id: \.offset) { _, event in
                Text(describe(event))
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal)
        .padding(.bottom, 8)
    }

    @ViewBuilder
    private var content: some View {
        switch state {
        case .loading:
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let message):
            ErrorBanner(message: message) { Task { await load() } }
                .padding()
        case .loaded(let status):
            statusGrid(status)
        }
    }

    private func statusGrid(_ status: StatusInfo) -> some View {
        ScrollView {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 12)], spacing: 12) {
                StatTile(title: "Files backed up", value: "\(status.filesTotal)",
                        detail: "\(status.syncCount) synced · \(status.archiveCount) archived")
                StatTile(title: "Total size", value: humanSize(status.totalBytes))
                StatTile(title: "Archive chunks", value: "\(status.chunksTotal)",
                        detail: "\(status.chunksPending.count) awaiting upload")
                StatTile(title: "Sync uploads pending", value: "\(status.syncPending)")
                StatTile(title: "Orphaned in cloud", value: "\(status.orphansPending)",
                        detail: "deleted locally, still remote")
                StatTile(title: "Dropzone pending", value: "\(status.dropzonePending)")
                StatTile(title: "Local chunk cache", value: humanSize(status.localCacheBytes))
                StatTile(title: "Last backup", value: humanTimestamp(status.lastBackup))
                StatTile(title: "Last upload", value: humanTimestamp(status.lastUpload))
            }
            .padding()
            Text("Remote: \(status.remoteFolder)")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .padding(.bottom)
        }
    }

    private func load() async {
        state = .loading
        do {
            state = .loaded(try await client.status())
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    private func runBackup() async {
        isRunning = true
        isPausing = false
        progressEvents = []
        runSummary = nil
        runFailed = false
        runPaused = false
        do {
            for try await event in await client.runBackup() {
                progressEvents.append(event)
                if event.isTerminal {
                    runFailed = event.ok != true
                    runPaused = event.paused == true
                    if event.paused == true {
                        runSummary = "Paused — \(event.syncUploaded ?? 0) synced, "
                            + "\(event.archiveUploaded ?? 0) chunk(s) uploaded so far. "
                            + "Run Backup again anytime to resume."
                    } else if event.ok == true {
                        runSummary = "Backup complete — \(event.syncUploaded ?? 0) synced, "
                            + "\(event.archiveUploaded ?? 0) chunk(s) uploaded."
                    } else {
                        runSummary = "Backup failed: \(event.error ?? "unknown error")"
                    }
                }
            }
        } catch {
            runFailed = true
            runSummary = "Backup failed: \(error.localizedDescription)"
        }
        isRunning = false
        isPausing = false
        await load()
    }

    private func pauseBackup() async {
        isPausing = true
        await client.requestPause()
    }

    private func describe(_ event: ProgressEvent) -> String {
        switch event.event {
        case "diff":
            return "Found \(event.added ?? 0) new, \(event.changed ?? 0) changed, "
                + "\(event.deleted ?? 0) deleted, \(event.dropzone ?? 0) to archive"
        case "chunk_build":
            return "Built chunk \(event.name ?? "") (\(event.index ?? 0)/\(event.total ?? 0))"
        case "sync_upload_start":
            return "Uploading \(event.count ?? 0) synced file(s)…"
        case "sync_file_uploaded":
            return "Uploaded \(event.arcname ?? "") (\(event.index ?? 0)/\(event.total ?? 0))"
        case "archive_upload_start":
            return "Uploading archive chunks…"
        case "chunk_uploaded":
            return "Uploaded chunk \(event.name ?? "") (\(event.index ?? 0)/\(event.total ?? 0))"
        case "dropzone_trashed":
            return "Archived and cleared: \(event.name ?? "")"
        case "result":
            if event.paused == true { return "Paused." }
            return event.ok == true ? "Done." : "Failed: \(event.error ?? "unknown error")"
        default:
            return event.event
        }
    }
}

private struct StatTile: View {
    let title: String
    let value: String
    var detail: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.title3.monospacedDigit().bold())
            if let detail {
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
    }
}

struct ErrorBanner: View {
    let message: String
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Couldn't reach backup-organizer", systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Text(message).font(.callout).foregroundStyle(.secondary)
            Button("Retry", action: retry)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 10))
    }
}
