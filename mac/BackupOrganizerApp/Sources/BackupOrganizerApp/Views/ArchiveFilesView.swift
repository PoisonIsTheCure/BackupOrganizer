import SwiftUI

/// "Archive Files/Folders…" button: picks file(s)/folder(s) via NSOpenPanel,
/// moves them into the dropzone and runs the backup cycle, then — if any
/// just-archived file still has a confirmed-uploaded synced twin — offers
/// to retire the synced copy via the same confirmation dialog ArchivedView
/// uses for the general case.
struct ArchiveFilesButton: View {
    let client: BackendClient
    let onCompleted: () -> Void

    @State private var isRunning = false
    @State private var error: String?
    @State private var retirePrompt: RetireSyncPrompt?

    var body: some View {
        Button {
            let paths = FilePicker.choose(canChooseFiles: true, canChooseDirectories: true,
                                          prompt: "Archive")
            guard !paths.isEmpty else { return }
            Task { await archive(paths) }
        } label: {
            Label("Archive Files/Folders…", systemImage: "archivebox")
        }
        .disabled(isRunning)
        .confirmRetireSync(prompt: $retirePrompt) { archiveArcnames in
            Task { await retireAfterArchive(archiveArcnames) }
        }
        .alert("Couldn't archive", isPresented: Binding(
            get: { error != nil }, set: { if !$0 { error = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(error ?? "")
        }
    }

    private func archive(_ paths: [String]) async {
        isRunning = true
        defer { isRunning = false }
        do {
            let result = try await client.archive(paths: paths)
            onCompleted()
            let confirmedTwins = result.relocatable.filter(\.twinConfirmed)
            if !confirmedTwins.isEmpty {
                retirePrompt = RetireSyncPrompt(
                    archiveArcnames: confirmedTwins.map(\.archiveArcname),
                    summaryLines: confirmedTwins.map { "\($0.archiveArcname) ↔ \($0.syncArcname)" })
            }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func retireAfterArchive(_ archiveArcnames: [String]) async {
        do {
            let result = try await client.retireSyncTwin(arcnames: archiveArcnames)
            if let skip = result.skipped.first {
                error = skip.reason
            }
            onCompleted()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
