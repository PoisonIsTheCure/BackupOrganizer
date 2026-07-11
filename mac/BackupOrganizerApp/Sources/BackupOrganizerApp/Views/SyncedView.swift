import SwiftUI

struct SyncedView: View {
    let client: BackendClient
    @State private var syncDirsState: LoadState<[String]> = .loading
    @State private var filesState: LoadState<[FileEntry]> = .loading
    @State private var orphansState: LoadState<[OrphanEntry]> = .loading
    @State private var pendingDelete: OrphanEntry?
    @State private var deletingArcname: String?
    @State private var pendingRemoveDir: String?
    @State private var removingDir: String?
    @State private var actionError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            List {
                syncDirsSection
                orphansSection
                filesSection
            }
            .listStyle(.inset)
        }
        .task { await loadAll() }
        .confirmDeleteRemote(orphan: $pendingDelete) { orphan in
            Task { await deleteRemote(orphan) }
        }
        .confirmationDialog(
            "Stop syncing this folder?",
            isPresented: Binding(get: { pendingRemoveDir != nil }, set: { if !$0 { pendingRemoveDir = nil } }),
            presenting: pendingRemoveDir
        ) { dir in
            Button("Stop Syncing", role: .destructive) {
                Task { await removeSync(dir) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { dir in
            Text("\(dir)\n\nThis folder stops being watched for changes. Its files already on "
                + "the cloud become orphans — kept there until you manually delete them (see "
                + "the orphans list below). Nothing local is deleted.")
        }
        .alert("Couldn't complete that action", isPresented: Binding(
            get: { actionError != nil }, set: { if !$0 { actionError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(actionError ?? "")
        }
    }

    private var header: some View {
        HStack {
            Text("Synced").font(.title2.bold())
            Spacer()
            AddSyncButton(client: client) { Task { await loadAll() } }
            Button {
                Task { await loadAll() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
        }
        .padding()
    }

    // MARK: - Synced folders

    @ViewBuilder
    private var syncDirsSection: some View {
        switch syncDirsState {
        case .loading:
            Section("Synced Folders") { ProgressView() }
        case .failed(let message):
            Section("Synced Folders") { Text(message).foregroundStyle(.secondary) }
        case .loaded(let dirs):
            Section("Synced Folders (\(dirs.count))") {
                if dirs.isEmpty {
                    Text("No folders configured — use Add Sync Folder above.")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(dirs, id: \.self) { dir in
                        HStack {
                            Image(systemName: "folder.fill").foregroundStyle(.secondary)
                            Text(dir).font(.body.monospaced())
                            Spacer()
                            if removingDir == dir {
                                ProgressView().controlSize(.small)
                            } else {
                                Button("Stop Syncing", role: .destructive) {
                                    pendingRemoveDir = dir
                                }
                                .disabled(removingDir != nil)
                            }
                        }
                    }
                }
            }
        }
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
                            Text("deleted locally \(humanTimestamp(orphan.deletedAt)) · \(humanSize(orphan.size))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if deletingArcname == orphan.arcname {
                            ProgressView().controlSize(.small)
                        } else {
                            Button("Delete from Cloud", role: .destructive) {
                                pendingDelete = orphan
                            }
                            .disabled(deletingArcname != nil)
                        }
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
                } else {
                    FileTreeView(root: buildFileTree(files)) { file in
                        UploadBadge(uploaded: file.isUploaded)
                    }
                }
            }
        }
    }

    private func loadAll() async {
        async let dirs: () = loadSyncDirs()
        async let files: () = loadFiles()
        async let orphans: () = loadOrphans()
        _ = await (dirs, files, orphans)
    }

    private func loadSyncDirs() async {
        syncDirsState = .loading
        do {
            syncDirsState = .loaded(try await client.status().syncDirs ?? [])
        } catch {
            syncDirsState = .failed(error.localizedDescription)
        }
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

    private func deleteRemote(_ orphan: OrphanEntry) async {
        deletingArcname = orphan.arcname
        defer { deletingArcname = nil }
        do {
            let result = try await client.deleteRemote(arcnames: [orphan.arcname])
            if let message = result.errors[orphan.arcname] {
                actionError = message
            } else if result.notFound.contains(orphan.arcname) {
                actionError = "\(orphan.arcname) is no longer an orphan (it may have reappeared locally)."
            }
            await loadOrphans()
        } catch {
            actionError = error.localizedDescription
        }
    }

    private func removeSync(_ dir: String) async {
        removingDir = dir
        defer { removingDir = nil }
        do {
            let result = try await client.removeSync(dirs: [dir])
            if !result.notFound.isEmpty {
                actionError = "\(dir) wasn't a configured sync folder (already removed?)."
            }
            await loadAll()
        } catch {
            actionError = error.localizedDescription
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
