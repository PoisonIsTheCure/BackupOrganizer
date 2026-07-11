import SwiftUI

struct DashboardView: View {
    let client: BackendClient
    @State private var state: LoadState<StatusInfo> = .loading

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            content
        }
        .task { await load() }
    }

    private var header: some View {
        HStack {
            Text("Dashboard").font(.title2.bold())
            Spacer()
            Button {
                Task { await load() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            Button {
                // Wired up in Phase 3 once `run --json` streams progress.
            } label: {
                Label("Run Backup", systemImage: "play.fill")
            }
            .disabled(true)
            .help("Coming in Phase 3, once live progress streaming is wired up.")
        }
        .padding()
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
                StatTile(title: "Last backup", value: status.lastBackup.isEmpty ? "never" : status.lastBackup)
                StatTile(title: "Last upload", value: status.lastUpload.isEmpty ? "never" : status.lastUpload)
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
