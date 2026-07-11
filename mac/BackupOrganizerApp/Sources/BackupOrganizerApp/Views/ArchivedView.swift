import SwiftUI

struct ArchivedView: View {
    let client: BackendClient
    @State private var state: LoadState<[FileEntry]> = .loading
    @State private var retirePrompt: RetireSyncPrompt?
    @State private var retiringArcname: String?
    @State private var actionError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            content
        }
        .task { await load() }
        .confirmRetireSync(prompt: $retirePrompt) { archiveArcnames in
            Task { await retireSyncTwin(archiveArcnames) }
        }
        .alert("Couldn't retire the synced copy", isPresented: Binding(
            get: { actionError != nil }, set: { if !$0 { actionError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(actionError ?? "")
        }
    }

    private var header: some View {
        HStack {
            Text("Archived").font(.title2.bold())
            Spacer()
            Button {
                Task { await load() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
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
        case .loaded(let files):
            List(files) { file in
                HStack {
                    VStack(alignment: .leading) {
                        Text(file.arcname).font(.body.monospaced())
                        Text("\(humanSize(file.size)) · \(file.chunk ?? "?")")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if file.relocatable == true {
                        if retiringArcname == file.arcname {
                            ProgressView().controlSize(.small)
                        } else {
                            Button("Retire Synced Copy") {
                                retirePrompt = RetireSyncPrompt(
                                    archiveArcnames: [file.arcname],
                                    summaryLines: [file.arcname])
                            }
                            .disabled(file.twinConfirmed != true || retiringArcname != nil)
                            .help(file.twinConfirmed == true
                                  ? "Remove the synced copy of this file, keeping only the archive."
                                  : "Waiting for the archive copy's upload to be confirmed first.")
                        }
                    }
                    UploadBadge(uploaded: file.isUploaded)
                }
            }
            .listStyle(.inset)
            if files.isEmpty {
                Text("Nothing archived yet.")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private func load() async {
        state = .loading
        do {
            state = .loaded(try await client.list(kind: "archive"))
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    private func retireSyncTwin(_ archiveArcnames: [String]) async {
        retiringArcname = archiveArcnames.first
        defer { retiringArcname = nil }
        do {
            let result = try await client.retireSyncTwin(arcnames: archiveArcnames)
            if let skip = result.skipped.first {
                actionError = skip.reason
            }
            await load()
        } catch {
            actionError = error.localizedDescription
        }
    }
}
