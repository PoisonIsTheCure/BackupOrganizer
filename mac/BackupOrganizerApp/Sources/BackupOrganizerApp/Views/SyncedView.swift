import SwiftUI

struct SyncedView: View {
    let client: BackendClient
    @State private var filesState: LoadState<[FileEntry]> = .loading
    @State private var orphansState: LoadState<[OrphanEntry]> = .loading

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            List {
                orphansSection
                filesSection
            }
            .listStyle(.inset)
        }
        .task { await loadAll() }
    }

    private var header: some View {
        HStack {
            Text("Synced").font(.title2.bold())
            Spacer()
            Button {
                Task { await loadAll() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
        }
        .padding()
    }

    @ViewBuilder
    private var orphansSection: some View {
        switch orphansState {
        case .loading:
            Section("Orphaned in the cloud") { ProgressView() }
        case .failed(let message):
            Section("Orphaned in the cloud") { Text(message).foregroundStyle(.secondary) }
        case .loaded(let orphans) where !orphans.isEmpty:
            Section("Orphaned in the cloud (\(orphans.count))") {
                ForEach(orphans) { orphan in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(orphan.arcname).font(.body.monospaced())
                            Text("deleted locally \(orphan.deletedAt) · \(humanSize(orphan.size))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Delete from Cloud", role: .destructive) {
                            // Wired up in Phase 3 with a confirmation dialog.
                        }
                        .disabled(true)
                        .help("Coming in Phase 3, behind a confirmation dialog.")
                    }
                }
            }
        case .loaded:
            EmptyView()
        }
    }

    @ViewBuilder
    private var filesSection: some View {
        switch filesState {
        case .loading:
            Section("Synced files") { ProgressView() }
        case .failed(let message):
            Section("Synced files") {
                ErrorBanner(message: message) { Task { await loadFiles() } }
            }
        case .loaded(let files):
            Section("Synced files (\(files.count))") {
                if files.isEmpty {
                    Text("Nothing synced yet.").foregroundStyle(.secondary)
                }
                ForEach(files) { file in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(file.arcname).font(.body.monospaced())
                            Text(humanSize(file.size)).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        UploadBadge(uploaded: file.isUploaded)
                    }
                }
            }
        }
    }

    private func loadAll() async {
        async let files: () = loadFiles()
        async let orphans: () = loadOrphans()
        _ = await (files, orphans)
    }

    private func loadFiles() async {
        filesState = .loading
        do {
            filesState = .loaded(try await client.list(kind: "sync"))
        } catch {
            filesState = .failed(error.localizedDescription)
        }
    }

    private func loadOrphans() async {
        orphansState = .loading
        do {
            orphansState = .loaded(try await client.orphans())
        } catch {
            orphansState = .failed(error.localizedDescription)
        }
    }
}

struct UploadBadge: View {
    let uploaded: Bool

    var body: some View {
        Text(uploaded ? "Uploaded" : "Pending")
            .font(.caption.bold())
            .padding(.horizontal, 8).padding(.vertical, 2)
            .background(uploaded ? .green.opacity(0.2) : .orange.opacity(0.2),
                       in: Capsule())
            .foregroundStyle(uploaded ? .green : .orange)
    }
}
