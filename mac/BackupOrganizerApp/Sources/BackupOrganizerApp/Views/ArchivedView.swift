import SwiftUI

struct ArchivedView: View {
    let client: BackendClient
    @State private var state: LoadState<[FileEntry]> = .loading

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
                        Button("Retire Synced Copy") {
                            // Wired up in Phase 3 with a confirmation dialog.
                        }
                        .disabled(true)
                        .help(file.twinConfirmed == true
                              ? "Coming in Phase 3, behind a confirmation dialog."
                              : "Waiting for the archive copy's upload to be confirmed first.")
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
}
